"""The safety channel, carried over PROFIsafe framing imported from P2.

Until now `SAFETY_OK` and `GUARD_CLOSED` were plain Modbus bits. That is honest
for a simulation and it is not how a real cell works: safety signals travel over
a protocol with its own checksum, sequence number and watchdog, precisely so
that a corrupted, repeated, lost or delayed message cannot be mistaken for a
healthy one.

That protocol is not reimplemented here. It is imported from
`fault-injection-harness`, pinned to a tag, where it was built and then attacked
with a catalogue of communication faults. Copying it would fork the thing that
was verified, so the evidence would no longer refer to what is running. This is
the third link in a chain where each depends on the last by pin and not by
paste: P1 is P2's device under test, P2's protection layer is P4's safety
channel.

THE PROPERTY THIS BUYS, and it is the only one that matters: **anything the
protection layer refuses reads as UNSAFE.** Not as an error to be logged, not as
a value to be retried, not as the last known good state. A safety channel that
cannot prove what it is saying is a safety channel saying no.

WHAT THIS IS NOT. Real PROFIsafe is a certified protocol stack with an F-Host,
an F-Device, and an F_Destination_Address administered so that no two devices on
a plant share one. This carries the same framing over an in-process call. It
demonstrates the mechanism and the reaction; it is not a safety network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

# The harness ships no py.typed marker, so mypy strict cannot see into it.
# Ignored at the import and narrowly, so a genuine typo in one of these names
# still fails rather than being swallowed by a blanket ignore.
from fih.protection import (  # type: ignore[import-untyped]
    PROFISAFE,
    PeerEndpoint,
    ProtectedTransport,
    ProtectionError,
)

#: The one request the channel answers. A safety link with a rich vocabulary is
#: a safety link with more ways to be wrong.
STATUS = "STATUS"

_GUARD = "GUARD"
_TORQUE = "TORQUE"


class SafetySource(Protocol):
    """Whatever actually knows the safety state. The plant, here."""

    guard_closed: bool
    torque_available: bool


@dataclass(frozen=True)
class SafetyState:
    """What the controller believes, and whether it is entitled to believe it."""

    guard_closed: bool
    torque_available: bool
    #: Why the channel is not trusted, or None when it is. Carried rather than
    #: raised so the controller can report a stop reason instead of only
    #: stopping, which is the difference between a machine that halts and a
    #: machine somebody can diagnose without opening it.
    refusal: str | None = None

    @property
    def trusted(self) -> bool:
        return self.refusal is None

    @classmethod
    def unsafe(cls, refusal: str) -> SafetyState:
        """The only state a refused message may produce.

        Both signals false, because de-energised is what an unproven safety
        signal means. Returning the last known values here would be the exact
        defect the Modbus channel had with 'keep last value'.
        """
        return cls(guard_closed=False, torque_available=False, refusal=refusal)


class Responder:
    """The safety channel's own side of the link.

    Separate from the reading side on purpose: both ends of a real F-link run
    the protection library, and a fault is injected BETWEEN them. Collapsing the
    two would leave nothing for a fault to corrupt, and the tests would be
    checking that a function returns what it was given.
    """

    def __init__(self, source: SafetySource) -> None:
        self._source = source

    def request(self, line: str) -> str:
        if line != STATUS:
            raise ValueError(f"the safety channel answers {STATUS!r}, not {line!r}")
        return (f"{_GUARD}={int(self._source.guard_closed)},"
                f"{_TORQUE}={int(self._source.torque_available)}")


class SafetyLink:
    """The controller's view of the safety channel, protected end to end.

    WHERE A FAULT GOES, and getting this wrong makes the whole thing decorative.
    The stack is:

        ProtectedTransport  <- the controller's end, checks the frame
          wire              <- the fault belongs HERE, on the protected frame
            PeerEndpoint    <- the channel's end, wraps its answer
              responder     <- the channel itself

    A fault injected between `PeerEndpoint` and `responder` corrupts the payload
    BEFORE it is wrapped, so the checksum is computed over the corrupted value
    and the frame arrives perfectly valid. The first version of this class did
    exactly that, and three tests that should have caught it passed instead. A
    protection layer tested that way proves nothing at all.

    `responder` replaces the channel itself, which is where a masquerade lives:
    a correctly framed answer about the wrong thing. `wire` sits on the frame,
    which is where corruption, repetition, loss and delay live.
    """

    def __init__(self, source: SafetySource,
                 responder: object | None = None,
                 wire: Callable[[object], object] | None = None) -> None:
        peer = PeerEndpoint(
            responder if responder is not None else Responder(source),
            PROFISAFE)
        self._transport = ProtectedTransport(
            wire(peer) if wire is not None else peer, PROFISAFE)

    def read(self) -> SafetyState:
        """Ask the channel, and believe it only if the framing holds.

        Every failure mode collapses to the same answer. That is deliberate: a
        controller that reacted differently to a bad checksum than to a stale
        counter would be making a safety decision out of a diagnosis, and the
        diagnosis is for the person, not for the machine.
        """
        try:
            reply = self._transport.request(STATUS)
        except ProtectionError as refused:
            return SafetyState.unsafe(refused.args[0])
        except Exception as broken:                # a wire that is not a wire
            return SafetyState.unsafe(f"UNREADABLE: {broken}")

        try:
            fields = dict(part.split("=", 1) for part in reply.split(","))
            return SafetyState(guard_closed=fields[_GUARD] == "1",
                               torque_available=fields[_TORQUE] == "1")
        except (ValueError, KeyError):
            # A frame that passed the checksum and says something this channel
            # does not understand is still a frame it cannot act on.
            return SafetyState.unsafe("MALFORMED")
